"""Reusable OOMP project factory; all new projects share prototype defaults."""
from .prototype import project_settings


def project_option(*, github_user: str, repository: str, kicad_name: str,
                   settings: dict | None = None, description: str = "Prototype PCB") -> dict:
    return {"taxonomy_1": "oomp", "taxonomy_2": "project", "taxonomy_3": "github",
            "taxonomy_4": github_user, "taxonomy_5": repository,
            "project_github_user": github_user, "project_github_repository": repository,
            "description": description,
            "kicad_details": {"kicad_name": kicad_name, "settings": project_settings(overrides=settings)}}
