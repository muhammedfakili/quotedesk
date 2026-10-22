from __future__ import annotations

import pytest

from app.catalogue.index import get_catalogue
from app.catalogue_client import LocalCatalogue
from app.config import settings


@pytest.fixture(scope="session")
def catalogue():
    cat = get_catalogue()
    cat.warm()
    return cat


@pytest.fixture(scope="session")
def search(catalogue):
    return LocalCatalogue(catalogue)


@pytest.fixture
def tmp_data(tmp_path, monkeypatch):
    """Point the runtime folders (db, inbox, outbox, quotes) at a temp dir; the model cache stays."""
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path
