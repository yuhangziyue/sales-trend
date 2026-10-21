from __future__ import annotations

import pytest

from salestrend.classify.rules import load_rulesets
from salestrend.ingest.pipeline import process, read_xlsx
from salestrend.settings import ROOT, get_settings


@pytest.fixture(scope="session")
def settings():
    return get_settings()


@pytest.fixture(scope="session")
def rows():
    return read_xlsx(ROOT / "data" / "sample-sales-data.xlsx")


@pytest.fixture(scope="session")
def events(rows, settings):
    return process(rows, settings.platforms)


@pytest.fixture(scope="session")
def rulesets(settings):
    return load_rulesets(settings.rules_dir)
