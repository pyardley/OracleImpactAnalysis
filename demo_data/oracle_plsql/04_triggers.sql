-- Ported from SQL Server RetailReportingDemo.dbo.trg_* - see 01_functions.sql
-- header. T-SQL's statement-level triggers (operating on the `inserted`/
-- `deleted` virtual tables, one call per batch) become Oracle FOR EACH ROW
-- triggers (:NEW/:OLD, one firing per row) - the natural idiom on each
-- platform for the same intent. AUDITLOG.AUDITID/CHANGEDAT/CHANGEDBY are all
-- defaulted (identity, SYSTIMESTAMP, USER) so audit inserts only need to
-- supply TABLENAME/OPERATION/RECORDID.

CREATE OR REPLACE TRIGGER TRG_CUSTOMERS_AUDIT
AFTER UPDATE ON CUSTOMERS
FOR EACH ROW
BEGIN
    INSERT INTO AUDITLOG (TABLENAME, OPERATION, RECORDID)
    VALUES ('Customers', 'UPDATE', :NEW.CUSTOMERID);
END TRG_CUSTOMERS_AUDIT;
/

CREATE OR REPLACE TRIGGER TRG_ORDERS_AUDIT
AFTER INSERT OR UPDATE OR DELETE ON ORDERS
FOR EACH ROW
DECLARE
    v_operation VARCHAR2(10);
    v_record_id NUMBER;
BEGIN
    IF INSERTING THEN
        v_operation := 'INSERT';
        v_record_id := :NEW.ORDERID;
    ELSIF UPDATING THEN
        v_operation := 'UPDATE';
        v_record_id := :NEW.ORDERID;
    ELSE
        v_operation := 'DELETE';
        v_record_id := :OLD.ORDERID;
    END IF;

    INSERT INTO AUDITLOG (TABLENAME, OPERATION, RECORDID)
    VALUES ('Orders', v_operation, v_record_id);
END TRG_ORDERS_AUDIT;
/

-- Oracle's BEFORE-trigger-mutates-:NEW pattern replaces T-SQL's
-- "IF UPDATE(ModifiedDate) RETURN" recursion guard entirely: mutating :NEW in
-- a BEFORE trigger can't re-fire the same UPDATE, so no guard is needed.
CREATE OR REPLACE TRIGGER TRG_ORDERS_SETMODIFIEDDATE
BEFORE UPDATE ON ORDERS
FOR EACH ROW
BEGIN
    :NEW.MODIFIEDDATE := SYSTIMESTAMP;
END TRG_ORDERS_SETMODIFIEDDATE;
/

-- T-SQL ranked all inserted rows together via ROW_NUMBER() OVER (PARTITION BY
-- OrderLineID ...); a FOR EACH ROW trigger already only sees one row, so the
-- ranking collapses to a plain "pick the region warehouse with the most
-- stock" lookup for :NEW's own product.
CREATE OR REPLACE TRIGGER TRG_ORDERLINES_DECREMENTINVENTORY
AFTER INSERT ON ORDERLINES
FOR EACH ROW
DECLARE
    v_warehouse_id INVENTORY.WAREHOUSEID%TYPE;
BEGIN
    SELECT i.WAREHOUSEID INTO v_warehouse_id
    FROM INVENTORY i
    JOIN ORDERS o     ON o.ORDERID = :NEW.ORDERID
    JOIN CUSTOMERS c  ON c.CUSTOMERID = o.CUSTOMERID
    JOIN WAREHOUSES w ON w.REGIONID = c.REGIONID AND w.WAREHOUSEID = i.WAREHOUSEID
    WHERE i.PRODUCTID = :NEW.PRODUCTID
    ORDER BY i.QUANTITYONHAND DESC
    FETCH FIRST 1 ROW ONLY;

    UPDATE INVENTORY
    SET QUANTITYONHAND = CASE WHEN QUANTITYONHAND - :NEW.QUANTITY < 0 THEN 0 ELSE QUANTITYONHAND - :NEW.QUANTITY END
    WHERE PRODUCTID = :NEW.PRODUCTID AND WAREHOUSEID = v_warehouse_id;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        NULL; -- no matching warehouse in the customer's region for this product
END TRG_ORDERLINES_DECREMENTINVENTORY;
/

CREATE OR REPLACE TRIGGER TRG_RETURNS_RESTOCKINVENTORY
AFTER INSERT ON RETURNS
FOR EACH ROW
DECLARE
    v_warehouse_id INVENTORY.WAREHOUSEID%TYPE;
    v_product_id   INVENTORY.PRODUCTID%TYPE;
BEGIN
    SELECT ol.PRODUCTID INTO v_product_id
    FROM ORDERLINES ol
    WHERE ol.ORDERLINEID = :NEW.ORDERLINEID;

    SELECT i.WAREHOUSEID INTO v_warehouse_id
    FROM INVENTORY i
    JOIN ORDERLINES ol ON ol.ORDERLINEID = :NEW.ORDERLINEID
    JOIN ORDERS o      ON o.ORDERID = ol.ORDERID
    JOIN CUSTOMERS c   ON c.CUSTOMERID = o.CUSTOMERID
    JOIN WAREHOUSES w  ON w.REGIONID = c.REGIONID AND w.WAREHOUSEID = i.WAREHOUSEID
    WHERE i.PRODUCTID = v_product_id
    ORDER BY i.QUANTITYONHAND ASC
    FETCH FIRST 1 ROW ONLY;

    UPDATE INVENTORY
    SET QUANTITYONHAND = QUANTITYONHAND + :NEW.QUANTITY
    WHERE PRODUCTID = v_product_id AND WAREHOUSEID = v_warehouse_id;
EXCEPTION
    WHEN NO_DATA_FOUND THEN
        NULL; -- no matching warehouse in the customer's region for this product
END TRG_RETURNS_RESTOCKINVENTORY;
/
