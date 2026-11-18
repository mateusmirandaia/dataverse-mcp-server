import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_trailing_slash_is_removed_and_urls_derived():
    settings = make_settings(url="https://contoso-test.crm.dynamics.com/")
    assert settings.url == "https://contoso-test.crm.dynamics.com"
    assert settings.api_base_url == "https://contoso-test.crm.dynamics.com/api/data/v9.2/"
    assert settings.scope == "https://contoso-test.crm.dynamics.com/.default"


@pytest.mark.parametrize(
    "url",
    [
        "http://contoso-test.crm.dynamics.com",
        "https://contoso-test.crm.dynamics.com/api/data/v9.2",
        "https://contoso-test.crm.dynamics.com/main.aspx?appid=1",
        "contoso-test.crm.dynamics.com",
    ],
)
def test_rejects_urls_that_are_not_an_environment_root(url):
    with pytest.raises(ValidationError):
        make_settings(url=url)


def test_writes_are_off_by_default():
    assert make_settings().allow_writes is False


def test_default_top_cannot_exceed_max_top():
    with pytest.raises(ValidationError):
        make_settings(default_top=100, max_top=50)


def test_max_top_respects_dataverse_page_limit():
    with pytest.raises(ValidationError):
        make_settings(max_top=10_000)


def test_secret_is_not_leaked_in_repr():
    assert "not-a-real-secret" not in repr(make_settings())
