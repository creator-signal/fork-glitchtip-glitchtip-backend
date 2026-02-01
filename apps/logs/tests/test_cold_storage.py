"""
Tests for cold storage functionality with pg_duckdb.

These tests are skipped when pg_duckdb is not available.
pg_duckdb requires shared_preload_libraries=pg_duckdb in postgresql.conf
and the extension installed. Tests check availability at runtime since
the test database may be configured differently than the main database.
"""

from datetime import timedelta

from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from glitchtip.partition_manager import UUID7Helper
from glitchtip.test_utils.test_case import GlitchTipTestCaseMixin

from ..cold_storage import ColdStorageConfig, is_pg_duckdb_available
from ..constants import LogLevel
from ..models import LogEvent


class ColdStorageConfigTestCase(TestCase):
    """Test cold storage configuration."""

    def test_config_from_settings(self):
        """Test loading config from Django settings."""
        config = ColdStorageConfig.from_settings()
        # Should have bucket from test settings or None
        self.assertIsInstance(config, ColdStorageConfig)

    @override_settings(GLITCHTIP_COLD_STORAGE_BUCKET="test-bucket")
    def test_config_custom_bucket(self):
        """Test custom bucket configuration."""
        config = ColdStorageConfig.from_settings()
        self.assertEqual(config.bucket, "test-bucket")


class ColdStoragePathTestCase(TestCase):
    """Test cold storage path generation (no pg_duckdb required)."""

    def test_org_cold_s3_path(self):
        """Test per-org S3 path generation."""
        from ..cold_storage import get_org_cold_s3_path

        config = ColdStorageConfig(
            bucket="my-bucket",
            endpoint_url=None,
            access_key_id=None,
            secret_access_key=None,
        )

        path = get_org_cold_s3_path(config, "logs_logevent", 123, "20260115")
        self.assertEqual(
            path, "s3://my-bucket/cold_storage/logs_logevent/org_123/20260115.parquet"
        )

    def test_org_cold_storage_path(self):
        """Test storage-relative path (without bucket)."""
        from ..cold_storage import get_org_cold_storage_path

        path = get_org_cold_storage_path("logs_logevent", 456, "20260120")
        self.assertEqual(path, "cold_storage/logs_logevent/org_456/20260120.parquet")


class ColdStorageQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test cold storage query functionality (requires pg_duckdb)."""

    def setUp(self):
        self.create_project()
        self.config = ColdStorageConfig.from_settings()

    def _skip_if_no_pg_duckdb(self):
        """Skip test if pg_duckdb is not available."""
        if not is_pg_duckdb_available():
            self.skipTest("pg_duckdb extension not available")

    def _skip_if_no_bucket(self):
        """Skip test if no bucket is configured."""
        if not self.config.bucket:
            self.skipTest("No cold storage bucket configured")

    def test_is_pg_duckdb_available_when_present(self):
        """Test pg_duckdb availability check returns True when available."""
        self._skip_if_no_pg_duckdb()
        # If we get here, pg_duckdb is available
        self.assertTrue(is_pg_duckdb_available())

    def test_setup_credentials(self):
        """Test S3 credentials setup doesn't error."""
        self._skip_if_no_pg_duckdb()
        self._skip_if_no_bucket()

        from ..cold_storage import setup_duckdb_s3_credentials

        # Should not raise
        setup_duckdb_s3_credentials(self.config)

    def test_query_empty_cold_storage(self):
        """Test querying cold storage when no files exist."""
        self._skip_if_no_pg_duckdb()
        self._skip_if_no_bucket()

        from ..cold_storage import query_cold_storage

        # Query for a non-existent org's data
        results = query_cold_storage(
            org_id=99999,
            start_date="20250101",
            end_date="20250101",
            config=self.config,
        )

        # Should return empty list, not error
        self.assertEqual(results, [])


class ColdStorageExportTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test cold storage export functionality (requires pg_duckdb)."""

    def setUp(self):
        self.create_project()
        self.config = ColdStorageConfig.from_settings()

    def _skip_if_no_pg_duckdb(self):
        """Skip test if pg_duckdb is not available."""
        if not is_pg_duckdb_available():
            self.skipTest("pg_duckdb extension not available")

    def _skip_if_no_bucket(self):
        """Skip test if no bucket is configured."""
        if not self.config.bucket:
            self.skipTest("No cold storage bucket configured")

    def test_archive_partition_per_org_empty(self):
        """Test archiving empty partition returns empty list."""
        self._skip_if_no_pg_duckdb()
        self._skip_if_no_bucket()

        from ..cold_storage import archive_partition_per_org

        # Archive a non-existent partition
        result = archive_partition_per_org(
            partition_name="logs_logevent_19700101",
            date_str="19700101",
            config=self.config,
        )

        # Should return empty list (partition doesn't exist or is empty)
        self.assertEqual(result, [])


class ColdStorageAPIQueryTestCase(GlitchTipTestCaseMixin, TransactionTestCase):
    """Test cold storage queries via API module."""

    def setUp(self):
        self.create_project()
        self.config = ColdStorageConfig.from_settings()

    def _skip_if_no_pg_duckdb(self):
        """Skip test if pg_duckdb is not available."""
        if not is_pg_duckdb_available():
            self.skipTest("pg_duckdb extension not available")

    def _skip_if_no_bucket(self):
        """Skip test if no bucket is configured."""
        if not self.config.bucket:
            self.skipTest("No cold storage bucket configured")

    def test_query_cold_storage_api_function(self):
        """Test the API's query_cold_storage function."""
        self._skip_if_no_pg_duckdb()
        self._skip_if_no_bucket()

        from ..api import query_cold_storage

        now = timezone.now()
        start = now - timedelta(days=30)

        # Query should return empty list for non-existent data
        results = query_cold_storage(
            organization_id=self.organization.id,
            start_dt=start,
            end_dt=now,
        )

        self.assertEqual(results, [])

    def test_query_logs_combined_hot_only(self):
        """Test combined query when all data is in hot storage."""
        from ..api import query_logs_combined

        now = timezone.now()

        # Create recent logs (hot storage only) with distinct timestamps
        for i in range(3):
            t = now - timedelta(minutes=i)
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(t),
                organization=self.organization,
                project=self.project,
                level=LogLevel.INFO,
                body=f"Recent log {i}",
            )

        # Query last 24 hours (all hot storage)
        start = now - timedelta(days=1)
        results = query_logs_combined(
            organization_id=self.organization.id,
            start_dt=start,
            end_dt=now + timedelta(minutes=1),  # Ensure we capture all logs
        )

        # Should find all 3 logs
        self.assertEqual(len(results), 3)

    def test_query_logs_combined_with_filters(self):
        """Test combined query with filters."""
        from ..api import query_logs_combined

        now = timezone.now()

        # Create logs with different levels and distinct timestamps
        for i, level in enumerate([LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR]):
            LogEvent.objects.create(
                id=UUID7Helper.from_datetime(now - timedelta(minutes=i)),
                organization=self.organization,
                project=self.project,
                level=level,
                body=f"Log level {level.label}",
            )

        start = now - timedelta(days=1)

        # Filter by error level
        results = query_logs_combined(
            organization_id=self.organization.id,
            start_dt=start,
            end_dt=now + timedelta(minutes=1),
            level_values=[LogLevel.ERROR],
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].level, LogLevel.ERROR)


class NoPgDuckDBTestCase(TestCase):
    """Tests that verify graceful behavior when pg_duckdb is unavailable."""

    def test_cold_storage_returns_empty_without_bucket(self):
        """Test that cold storage returns empty when no bucket configured."""
        from ..cold_storage import query_cold_storage

        # Config with no bucket should trigger early return
        config = ColdStorageConfig(
            bucket=None,
            endpoint_url=None,
            access_key_id=None,
            secret_access_key=None,
        )

        # Should return empty list, not error
        results = query_cold_storage(
            org_id=1,
            start_date="20260101",
            end_date="20260101",
            config=config,
        )
        self.assertEqual(results, [])

    def test_api_cold_storage_returns_empty_without_bucket(self):
        """Test that API cold storage returns empty when no bucket."""
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from ..api import query_cold_storage

        now = datetime.now(dt_timezone.utc)
        start = now - timedelta(days=1)

        # Without pg_duckdb or bucket, should return empty list
        results = query_cold_storage(
            organization_id=1,
            start_dt=start,
            end_dt=now,
        )
        # Either returns empty (no pg_duckdb) or empty (no bucket/no data)
        self.assertEqual(results, [])
