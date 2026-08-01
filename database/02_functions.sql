-- Ported from SQL Server RetailReportingDemo.dbo.fn_NetLineAmount / fn_FiscalPeriod
-- (see corporate-rag ingestion of that database). Deployed into Oracle RetailDemo
-- so OIA's own PL/SQL lineage parser has real procedural code to discover -
-- RetailDemo previously had zero views/procedures/functions/triggers.

CREATE OR REPLACE FUNCTION FN_NETLINEAMOUNT(
    p_quantity      IN NUMBER,
    p_unit_price    IN NUMBER,
    p_discount_pct  IN NUMBER
) RETURN NUMBER
IS
BEGIN
    RETURN ROUND(p_quantity * p_unit_price * (1 - (p_discount_pct / 100)), 2);
END FN_NETLINEAMOUNT;
/

CREATE OR REPLACE FUNCTION FN_FISCALPERIOD(
    p_as_of_date IN DATE
) RETURN VARCHAR2
IS
    v_fiscal_year NUMBER;
BEGIN
    -- Fiscal year starts July 1st: a date in Jan-Jun belongs to the fiscal
    -- year that started the previous July.
    IF EXTRACT(MONTH FROM p_as_of_date) >= 7 THEN
        v_fiscal_year := EXTRACT(YEAR FROM p_as_of_date) + 1;
    ELSE
        v_fiscal_year := EXTRACT(YEAR FROM p_as_of_date);
    END IF;

    RETURN 'FY' || TO_CHAR(v_fiscal_year) || '-' || LPAD(TO_CHAR(EXTRACT(MONTH FROM p_as_of_date)), 2, '0');
END FN_FISCALPERIOD;
/
