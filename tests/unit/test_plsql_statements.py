from oia.lineage.plsql_statements import harvest_statements


def test_select_into_and_insert_select():
    body = """
    BEGIN
        SELECT customer_id INTO v_id FROM customers WHERE email = :1;

        INSERT INTO staging_orders (order_id, customer_id)
        SELECT o.order_id, o.customer_id FROM orders o WHERE o.status = 'OPEN';
    END;
    """
    stmts = harvest_statements(body)
    assert [s.kind for s in stmts] == ["sql", "sql"]
    assert "INTO" not in stmts[0].sql_for_parsing.upper()
    assert "customers" in stmts[0].sql_for_parsing.lower()
    assert stmts[1].sql_for_parsing.strip().upper().startswith("INSERT")
    assert "SELECT" in stmts[1].sql_for_parsing.upper()


def test_dynamic_sql_literal_is_resolved():
    body = "BEGIN EXECUTE IMMEDIATE 'DELETE FROM logs WHERE id = 1'; END;"
    stmts = harvest_statements(body)
    assert len(stmts) == 1
    assert stmts[0].kind == "sql"
    assert stmts[0].sql_for_parsing == "DELETE FROM logs WHERE id = 1"


def test_dynamic_sql_concatenation_is_unresolved():
    body = "BEGIN EXECUTE IMMEDIATE 'DELETE FROM ' || p_table_name; END;"
    stmts = harvest_statements(body)
    assert len(stmts) == 1
    assert stmts[0].kind == "dynamic_sql"
    assert stmts[0].sql_for_parsing is None


def test_semicolon_inside_string_literal_not_split():
    body = "BEGIN INSERT INTO logs (msg) VALUES ('a; b; c'); END;"
    stmts = harvest_statements(body)
    assert len(stmts) == 1
    assert "a; b; c" in stmts[0].raw_text


def test_keyword_inside_comment_ignored():
    body = """
    BEGIN
        -- SELECT * FROM should_not_be_captured
        /* INSERT INTO also_should_not_be_captured VALUES (1) */
        UPDATE accounts SET balance = balance - 1 WHERE id = 1;
    END;
    """
    stmts = harvest_statements(body)
    assert len(stmts) == 1
    assert stmts[0].sql_for_parsing.strip().upper().startswith("UPDATE")


def test_nested_block_statements_all_captured_in_order():
    body = """
    BEGIN
      UPDATE a SET x = 1;
      BEGIN
        UPDATE b SET y = 2;
      EXCEPTION WHEN OTHERS THEN NULL;
      END;
      INSERT INTO c (z) VALUES (3);
    END;
    """
    stmts = harvest_statements(body)
    first_words = [s.raw_text.split()[0].upper() for s in stmts]
    assert first_words == ["UPDATE", "UPDATE", "INSERT"]


def test_empty_source_returns_no_statements():
    assert harvest_statements(None) == []
    assert harvest_statements("") == []


def test_merge_statement_captured_whole():
    body = """
    BEGIN
      MERGE INTO target t USING source s ON (t.id = s.id)
      WHEN MATCHED THEN UPDATE SET t.val = s.val
      WHEN NOT MATCHED THEN INSERT (id, val) VALUES (s.id, s.val);
    END;
    """
    stmts = harvest_statements(body)
    assert len(stmts) == 1
    assert stmts[0].kind == "sql"
    assert stmts[0].sql_for_parsing.strip().upper().startswith("MERGE")
