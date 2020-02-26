from django.db import models


class Team(models.Model):
    slug = models.SlugField()
    organization = models.ForeignKey(
        "organizations.Organization", on_delete=models.CASCADE, related_name="teams"
    )
    members = models.ManyToManyField("users.User")
    projects = models.ManyToManyField("projects.Project")
