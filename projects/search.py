"""What the projects app makes searchable: projects and their tasks."""
from root.search import Hit, SearchType, link, register, status

from .models import Project, ProjectTask

PROJECT_STATUS = {
    "C": ("Completed", "success"), "IP": ("In Progress", "warning"),
    "P": ("Planned", "neutral"), "X": ("Cancelled", "danger"),
}


def _project_hit(project):
    label, tone = PROJECT_STATUS.get(project.status, (project.status, "neutral"))
    return Hit(
        id=project.id,
        title=project.name,
        subtitle=getattr(project.customer, "name", ""),
        date=project.created_at,
        status=status(project.status, label, tone),
        link=link("/projects/view-project", project_id=project.id),
    )


register(SearchType(
    key="project",
    label="Projects",
    model=Project,
    module="projects",
    text_fields=["name", "desc", "customer__name"],
    number_fields=["id"],
    choice_fields={"status": None},
    select_related=["customer"],
    order_by=["-created_at", "-id"],
    priority=210,
    to_hit=_project_hit,
))

register(SearchType(
    key="project_task",
    label="Project tasks",
    model=ProjectTask,
    module="projects",
    business_field="project__business",
    text_fields=["name", "description", "project__name"],
    number_fields=["id"],
    select_related=["project"],
    order_by=["-end", "-id"],
    priority=220,
    to_hit=lambda task: Hit(
        id=task.id,
        title=task.name,
        subtitle=task.project.name,
        date=task.end,
        status=status("DONE", "Done", "success") if task.is_complete else None,
        link=link("/projects/update-task", project_id=task.project_id, task_id=task.id),
    ),
))
