-- ============================================================================
-- RetailDemo - populate derived tables (staging + report snapshots)
--
-- STAGINGCUSTOMERSEGMENT, STAGINGCOMPLETEDORDERLINES, and all REPORT_* tables
-- are wholly derived from the base tables (see 04_procedures.sql). Rather
-- than dumping their data as static INSERTs, this calls the same stored
-- procedures that maintain them in normal operation, so a freshly built
-- environment computes them exactly the way production does - and this
-- doubles as a smoke test that the deployed procedures actually run
-- correctly against the freshly loaded data.
--
-- Each BuildReport procedure internally calls USP_STAGECOMPLETEDORDERLINES
-- and/or USP_LOOKUPCUSTOMERSEGMENT as needed, so simply calling all five
-- (with their default ~24-month date window) is sufficient - order between
-- them doesn't matter, they're independent of each other.
-- ============================================================================

SET SERVEROUTPUT ON

BEGIN
    DBMS_OUTPUT.PUT_LINE('Building REPORT_CUSTOMERCHURNRISK...');
    USP_BUILDREPORT_CUSTOMERCHURNRISK();

    DBMS_OUTPUT.PUT_LINE('Building REPORT_EMPLOYEECOMMISSION...');
    USP_BUILDREPORT_EMPLOYEECOMMISSION();

    DBMS_OUTPUT.PUT_LINE('Building REPORT_INVENTORYREPLENISHMENT...');
    USP_BUILDREPORT_INVENTORYREPLENISHMENT();

    DBMS_OUTPUT.PUT_LINE('Building REPORT_MONTHLYSALESBYREGION...');
    USP_BUILDREPORT_MONTHLYSALESBYREGION();

    DBMS_OUTPUT.PUT_LINE('Building REPORT_PRODUCTPERFORMANCE...');
    USP_BUILDREPORT_PRODUCTPERFORMANCE();

    DBMS_OUTPUT.PUT_LINE('Derived tables populated.');
END;
/
