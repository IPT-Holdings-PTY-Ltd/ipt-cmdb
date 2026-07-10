"""Tests for IPT-CMDB."""

import pytest
from datetime import datetime, timezone

from cmdb import create_app
from cmdb.extensions import db as _db
from cmdb.models import Asset, Relationship, ChangeRecord


@pytest.fixture
def app():
    app = create_app(
        {
            "TESTING": True,
            "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",
        }
    )
    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def sample_assets(app):
    with app.app_context():
        a1 = Asset(name="web-server-01", asset_type="server", status="active", environment="production")
        a2 = Asset(name="db-server-01", asset_type="database", status="active", environment="production")
        a3 = Asset(name="app-service", asset_type="application", status="active", environment="production")
        _db.session.add_all([a1, a2, a3])
        _db.session.commit()
        return a1.id, a2.id, a3.id


# ---------------------------------------------------------------------------
# Asset tests
# ---------------------------------------------------------------------------

class TestAssets:
    def test_index_empty(self, client):
        r = client.get("/assets/")
        assert r.status_code == 200
        assert b"No assets" in r.data

    def test_create_asset(self, client):
        r = client.post(
            "/assets/new",
            data={
                "name": "server-01",
                "asset_type": "server",
                "status": "active",
                "environment": "production",
                "description": "",
                "owner": "Alice",
                "location": "DC1",
                "ip_address": "10.0.0.1",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"server-01" in r.data

    def test_create_asset_missing_name(self, client):
        r = client.post(
            "/assets/new",
            data={"asset_type": "server", "status": "active", "environment": "production"},
        )
        assert r.status_code == 200
        # stays on form page – no redirect
        assert b"New Asset" in r.data

    def test_asset_detail(self, client, sample_assets):
        asset_id = sample_assets[0]
        r = client.get(f"/assets/{asset_id}")
        assert r.status_code == 200
        assert b"web-server-01" in r.data

    def test_edit_asset(self, client, sample_assets):
        asset_id = sample_assets[0]
        r = client.post(
            f"/assets/{asset_id}/edit",
            data={
                "name": "web-server-01",
                "asset_type": "server",
                "status": "inactive",
                "environment": "production",
                "description": "updated",
                "owner": "",
                "location": "",
                "ip_address": "",
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"updated" in r.data

    def test_delete_asset(self, client, sample_assets):
        asset_id = sample_assets[0]
        r = client.post(f"/assets/{asset_id}/delete", follow_redirects=True)
        assert r.status_code == 200
        # The detail link for the deleted asset should no longer appear
        assert f'/assets/{asset_id}"'.encode() not in r.data

    def test_asset_list_shows_assets(self, client, sample_assets):
        r = client.get("/assets/")
        assert b"web-server-01" in r.data
        assert b"db-server-01" in r.data


# ---------------------------------------------------------------------------
# Relationship tests
# ---------------------------------------------------------------------------

class TestRelationships:
    def test_index_empty(self, client):
        r = client.get("/relationships/")
        assert r.status_code == 200
        assert b"No relationships" in r.data

    def test_create_relationship(self, client, sample_assets):
        a1_id, a2_id, _ = sample_assets
        r = client.post(
            "/relationships/new",
            data={"source_id": a1_id, "target_id": a2_id, "rel_type": "depends_on", "notes": ""},
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"depends_on" in r.data

    def test_create_relationship_same_asset(self, client, sample_assets):
        a1_id, _, _ = sample_assets
        r = client.post(
            "/relationships/new",
            data={"source_id": a1_id, "target_id": a1_id, "rel_type": "depends_on", "notes": ""},
            follow_redirects=True,
        )
        assert b"Source and target asset must be different" in r.data

    def test_delete_relationship(self, client, app, sample_assets):
        a1_id, a2_id, _ = sample_assets
        with app.app_context():
            rel = Relationship(source_id=a1_id, target_id=a2_id, rel_type="depends_on")
            _db.session.add(rel)
            _db.session.commit()
            rel_id = rel.id
        r = client.post(f"/relationships/{rel_id}/delete", follow_redirects=True)
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Change record tests
# ---------------------------------------------------------------------------

class TestChangeRecords:
    def test_index_empty(self, client):
        r = client.get("/changes/")
        assert r.status_code == 200
        assert b"No change records" in r.data

    def test_create_change_record(self, client, sample_assets):
        a1_id, a2_id, _ = sample_assets
        r = client.post(
            "/changes/new",
            data={
                "title": "Kernel update",
                "asset_id": a1_id,
                "change_type": "modification",
                "description": "Applied kernel patch",
                "changed_by": "Bob",
                "change_date": "2025-01-15T10:00",
                "status": "completed",
                "impacted_asset_ids": [a2_id],
            },
            follow_redirects=True,
        )
        assert r.status_code == 200
        assert b"Kernel update" in r.data

    def test_change_record_detail(self, client, app, sample_assets):
        a1_id, a2_id, _ = sample_assets
        with app.app_context():
            cr = ChangeRecord(
                title="Test change",
                asset_id=a1_id,
                change_type="modification",
                change_date=datetime.now(timezone.utc),
                status="planned",
            )
            _db.session.add(cr)
            _db.session.commit()
            cr_id = cr.id
        r = client.get(f"/changes/{cr_id}")
        assert r.status_code == 200
        assert b"Test change" in r.data

    def test_delete_change_record(self, client, app, sample_assets):
        a1_id, _, _ = sample_assets
        with app.app_context():
            cr = ChangeRecord(
                title="To delete",
                asset_id=a1_id,
                change_type="removal",
                change_date=datetime.now(timezone.utc),
                status="planned",
            )
            _db.session.add(cr)
            _db.session.commit()
            cr_id = cr.id
        r = client.post(f"/changes/{cr_id}/delete", follow_redirects=True)
        assert r.status_code == 200
        # The detail link for the deleted change record should no longer appear
        assert f'/changes/{cr_id}"'.encode() not in r.data


# ---------------------------------------------------------------------------
# Impact analysis tests
# ---------------------------------------------------------------------------

class TestImpactAnalysis:
    def test_impact_page_loads(self, client):
        r = client.get("/impact/")
        assert r.status_code == 200
        assert b"Impact Analysis" in r.data

    def test_no_dependents(self, client, sample_assets):
        a1_id, _, _ = sample_assets
        r = client.get(f"/impact/?asset_id={a1_id}")
        assert r.status_code == 200
        assert b"no downstream impact" in r.data.lower() or b"No other assets" in r.data

    def test_direct_dependency(self, client, app, sample_assets):
        a1_id, a2_id, _ = sample_assets
        # a1 depends_on a2: so if a2 changes, a1 is impacted
        with app.app_context():
            rel = Relationship(source_id=a1_id, target_id=a2_id, rel_type="depends_on")
            _db.session.add(rel)
            _db.session.commit()
        r = client.get(f"/impact/?asset_id={a2_id}")
        assert r.status_code == 200
        assert b"web-server-01" in r.data

    def test_transitive_dependency(self, client, app, sample_assets):
        a1_id, a2_id, a3_id = sample_assets
        # a1 depends_on a2, a2 depends_on a3 → if a3 changes, both a1 and a2 are impacted
        with app.app_context():
            _db.session.add_all([
                Relationship(source_id=a1_id, target_id=a2_id, rel_type="depends_on"),
                Relationship(source_id=a2_id, target_id=a3_id, rel_type="depends_on"),
            ])
            _db.session.commit()
        r = client.get(f"/impact/?asset_id={a3_id}")
        assert b"web-server-01" in r.data
        assert b"db-server-01" in r.data
