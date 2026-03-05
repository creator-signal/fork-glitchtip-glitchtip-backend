from django.core.management.base import BaseCommand
from django.db.models import Exists, OuterRef

from apps.files.models import File, FileBlob

BATCH_SIZE = 1000


class Command(BaseCommand):
    help = "Delete FileBlob rows (and their backing storage files) that have no File referencing them"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview what would be deleted without making changes",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]

        orphaned_qs = FileBlob.objects.filter(
            ~Exists(File.objects.filter(blob=OuterRef("pk")))
        )

        total = orphaned_qs.count()
        if total == 0:
            self.stdout.write("No orphaned FileBlobs found.")
            return

        if dry_run:
            self.stdout.write(f"[DRY RUN] Would delete {total} orphaned FileBlobs.")
            return

        deleted = 0
        while batch_ids := list(orphaned_qs.values_list("id", flat=True)[:BATCH_SIZE]):
            blobs = list(FileBlob.objects.filter(id__in=batch_ids))
            for blob in blobs:
                try:
                    blob.blob.delete(save=False)
                except Exception as e:
                    self.stderr.write(
                        f"Warning: failed to delete storage for FileBlob {blob.id}: {e}"
                    )
            FileBlob.objects.filter(id__in=[b.id for b in blobs]).delete()

            deleted += len(batch_ids)
            self.stdout.write(f"Deleted {deleted}/{total} orphaned FileBlobs...")

        self.stdout.write(
            self.style.SUCCESS(f"Done. Deleted {deleted} orphaned FileBlobs.")
        )
