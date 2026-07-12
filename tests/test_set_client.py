from app.config import Settings
from app.services.set_client import SetClient


def test_parse_json_preserves_string_trailing_zeros():
    client = SetClient(Settings(admin_api_key="test-secret-key", _env_file=None))
    quote = client._parse_json(
        {"data": {"index": "1,234.50", "marketValue": "56,789.00", "lastUpdate": "2026-07-10T10:00:00+07:00"}}
    )
    assert quote.index == "1234.50"
    assert quote.value == "56789.00"
    assert quote.source_timestamp.utcoffset() is not None
