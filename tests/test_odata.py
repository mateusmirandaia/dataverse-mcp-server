import pytest

from dataverse_mcp import odata
from dataverse_mcp.errors import InvalidQueryError


@pytest.mark.parametrize("name", ["account", "new_project", "_parentcustomerid_value", "Account"])
def test_valid_identifiers(name):
    assert odata.validate_identifier(name) == name


@pytest.mark.parametrize(
    "name", ["", "accounts?$top=1", "../WhoAmI", "account(1)", "1abc", "name eq 'x'", "a/b"]
)
def test_identifiers_that_could_alter_the_path_are_rejected(name):
    with pytest.raises(InvalidQueryError):
        odata.validate_identifier(name)


def test_guid_accepts_braces_and_normalises_case():
    raw = "{A1B2C3D4-0000-0000-0000-00000000ABCD}"
    assert odata.validate_guid(raw) == "a1b2c3d4-0000-0000-0000-00000000abcd"


def test_invalid_guid():
    with pytest.raises(InvalidQueryError, match="GUID"):
        odata.validate_guid("not-an-id")


def test_quote_string_doubles_single_quotes():
    assert odata.quote_string("O'Brien") == "'O''Brien'"


def test_build_select_accepts_list_or_csv_and_dedupes():
    assert odata.build_select(["name", "accountid", "name"]) == "name,accountid"
    assert odata.build_select("name, accountid") == "name,accountid"
    assert odata.build_select(None) is None
    assert odata.build_select([]) is None


def test_build_select_rejects_expressions():
    with pytest.raises(InvalidQueryError):
        odata.build_select(["name&$expand=*"])


def test_build_orderby():
    assert odata.build_orderby("createdon DESC, name") == "createdon desc,name"
    assert odata.build_orderby("  ") is None


@pytest.mark.parametrize("orderby", ["name sideways", "name; drop", "createdon desc asc"])
def test_build_orderby_rejects_bad_terms(orderby):
    with pytest.raises(InvalidQueryError):
        odata.build_orderby(orderby)


def test_check_filter_passes_real_expressions_through():
    expression = "statecode eq 0 and contains(name,'O''Brien')"
    assert odata.check_filter(expression) == expression
    assert odata.check_filter("") is None


@pytest.mark.parametrize(
    "expression",
    [
        "$filter=statecode eq 0",
        "?$filter=statecode eq 0",
        "https://contoso.crm.dynamics.com/api/data/v9.2/accounts",
        "statecode eq 0&$top=5000",
        "statecode eq 0\nor 1 eq 1",
        "x" * (odata.MAX_FILTER_LENGTH + 1),
    ],
)
def test_check_filter_rejects_things_that_are_not_a_filter(expression):
    with pytest.raises(InvalidQueryError):
        odata.check_filter(expression)


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(None, 10), (5, 5), (50, 50), (51, 50), (100_000, 50), (0, 1), (-3, 1)],
)
def test_clamp_top(requested, expected):
    assert odata.clamp_top(requested, default=10, maximum=50) == expected


def test_parse_entity_id():
    header = "https://contoso.crm.dynamics.com/api/data/v9.2/accounts(7D577253-3EF0-4A0A-BB7F-8335C2596E70)"
    assert odata.parse_entity_id(header) == "7d577253-3ef0-4a0a-bb7f-8335c2596e70"
    assert odata.parse_entity_id(None) is None
    assert odata.parse_entity_id("garbage") is None


def test_simplify_record_folds_annotations():
    record = {
        "@odata.etag": 'W/"123"',
        "name": "Contoso",
        "statuscode": 1,
        f"statuscode@{odata.FORMATTED_VALUE}": "Active",
        "_primarycontactid_value": "a1b2c3d4-0000-0000-0000-00000000abcd",
        f"_primarycontactid_value@{odata.FORMATTED_VALUE}": "Ana Souza",
        f"_primarycontactid_value@{odata.LOOKUP_LOGICAL_NAME}": "contact",
    }
    assert odata.simplify_record(record) == {
        "name": "Contoso",
        "statuscode": 1,
        "_primarycontactid_value": "a1b2c3d4-0000-0000-0000-00000000abcd",
        "_formatted": {"statuscode": "Active", "_primarycontactid_value": "Ana Souza"},
        "_lookup_table": {"_primarycontactid_value": "contact"},
    }


def test_validate_payload_accepts_columns_and_binds():
    body = {
        "name": "Contoso",
        "primarycontactid@odata.bind": "/contacts(a1b2c3d4-0000-0000-0000-00000000abcd)",
    }
    assert odata.validate_payload(body) == body


def test_validate_payload_explains_lookup_mistake():
    with pytest.raises(InvalidQueryError, match=r"@odata\.bind"):
        odata.validate_payload({"_primarycontactid_value": "a1b2c3d4-0000-0000-0000-00000000abcd"})


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bad column": 1},
        {"primarycontactid@odata.bind": "https://evil.example/contacts(1)"},
        {"primarycontactid@odata.bind": 42},
    ],
)
def test_validate_payload_rejects(payload):
    with pytest.raises(InvalidQueryError):
        odata.validate_payload(payload)
