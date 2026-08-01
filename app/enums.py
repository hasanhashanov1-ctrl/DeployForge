from enum import StrEnum


class ProjectState(StrEnum):
    ACTIVE = "active"
    DELETING = "deleting"


class LifecycleState(StrEnum):
    ACTIVE = "active"
    STOPPING = "stopping"
    STOPPED = "stopped"
    STARTING = "starting"
    ROLLING_BACK = "rolling_back"


LIFECYCLE_IN_PROGRESS_STATES = {
    LifecycleState.STOPPING,
    LifecycleState.STARTING,
    LifecycleState.ROLLING_BACK,
}


class DeploymentStatus(StrEnum):
    QUEUED = "queued"
    CLONING = "cloning"
    BUILDING = "building"
    STARTING = "starting"
    RUNNING = "running"
    FAILED = "failed"
    STOPPED = "stopped"
    CANCELLED = "cancelled"


IN_PROGRESS_STATUSES = {
    DeploymentStatus.QUEUED,
    DeploymentStatus.CLONING,
    DeploymentStatus.BUILDING,
    DeploymentStatus.STARTING,
}
