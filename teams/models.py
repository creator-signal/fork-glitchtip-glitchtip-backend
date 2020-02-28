from django.db import models


class Team(models.Model):
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    slug = models.SlugField()
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="teams"
    )
    members = models.ManyToManyField("users.User")
    projects = models.ManyToManyField("projects.Project")

    class Meta:
        unique_together = ("slug", "organization")
