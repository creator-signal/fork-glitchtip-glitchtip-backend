from django.core.management import call_command


def create_partitions(apps, schema_editor):
    """
    Create partitions for all partitioned models.

    Storage V2 Transition Note:
    - IssueEvent uses manual UUID-based partitioning (PartitionManager)
    - pgpartition doesn't understand UUID partitioning and will fail
    - We catch those specific errors and continue
    - psql_partition library will be removed in GlitchTip v7.0
    """
    from django.db.utils import DataError
    import logging

    logger = logging.getLogger(__name__)

    try:
        call_command("pgpartition", yes=True)
    except DataError as e:
        # Expected error during V2 transition: pgpartition tries to create
        # datetime partitions on UUID-partitioned IssueEvent table
        error_msg = str(e)
        if "invalid input syntax for type uuid" in error_msg.lower():
            logger.info(
                "Skipping pgpartition error for UUID-partitioned model "
                "(expected during Storage V2 transition). Other models were partitioned successfully."
            )
        else:
            # Unexpected DataError - re-raise it
            raise
