from django.db import models


class ArtifactBundle(models.Model):
    """
    Supports Artifact Bundles and Release Bundles
    """

    organization = models.ForeignKey(
        "organizations_ext.Organization", on_delete=models.CASCADE, related_name="+"
    )
    debug_id = models.UUIDField(blank=True, null=True)
    last_used = models.DateTimeField(auto_now=True, db_index=True)
    release = models.ForeignKey(
        "releases.release",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
    )
    minified_file = models.ForeignKey(
        "files.File", on_delete=models.SET_NULL, blank=True, null=True, related_name="+"
    )
    file = models.ForeignKey("files.File", on_delete=models.CASCADE, related_name="+")
    data = models.JSONField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "debug_id"], name="unique_org_debug_id"
            ),
        ]
