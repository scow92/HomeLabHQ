"""Compute workload discovery and detail routes."""
from urllib.parse import unquote

import services

from backend.http.responses import json_response
from backend.http.router import AuthPolicy, Route


def list_compute(request):
    return json_response(services.list_compute(request.require_actor()))


def detail(request):
    return json_response({"instance": services.compute_detail(
        request.require_actor(), request.params["compute_id"])})


def refresh(request):
    return json_response(services.refresh_compute(request.require_actor()))


def jobs(request):
    return json_response({"jobs": services.compute_jobs(
        request.require_actor(), request.params["compute_id"])})


def job(request):
    return json_response({"job": services.compute_job(
        request.require_actor(), request.params["job_id"])})


def check_updates(request):
    return json_response({"job": services.compute_check_updates(
        request.require_actor(), request.params["compute_id"])}, status=202)


def update(request):
    body = request.body
    allow_reboot = body.get("allowReboot", False)
    reboot_confirmed = body.get("rebootConfirmed", False)
    if not isinstance(allow_reboot, bool) or not isinstance(reboot_confirmed, bool):
        raise ValueError("reboot permission fields must be booleans")
    return json_response({"job": services.compute_update(
        request.require_actor(), request.params["compute_id"],
        allow_reboot=allow_reboot,
        reboot_confirmed=reboot_confirmed)}, status=202)


def docker_discover(request):
    return json_response({"job": services.compute_docker_discover(
        request.require_actor(), request.params["compute_id"])}, status=202)


def docker_check(request):
    return json_response({"job": services.compute_docker_check(
        request.require_actor(), request.params["compute_id"],
        request.body.get("projectName"))}, status=202)


def docker_strategy(request):
    return json_response({"project": services.compute_docker_strategy(
        request.require_actor(), request.params["compute_id"],
        unquote(request.params["project_name"]),
        request.body.get("mode", request.body.get("strategy")))})


def docker_update(request):
    return json_response({"job": services.compute_docker_update(
        request.require_actor(), request.params["compute_id"],
        unquote(request.params["project_name"]))}, status=202)


def routes():
    return (
        Route("GET", "/api/compute", list_compute, name="compute-list"),
        Route("POST", "/api/compute/refresh", refresh, AuthPolicy.ADMIN,
              "compute-refresh"),
        Route("GET", "/api/compute/jobs/{job_id}", job, name="compute-job"),
        Route("GET", "/api/compute/{compute_id}/jobs", jobs, name="compute-jobs"),
        Route("POST", "/api/compute/{compute_id}/updates/check", check_updates,
              name="compute-updates-check"),
        Route("POST", "/api/compute/{compute_id}/updates", update, AuthPolicy.ADMIN,
              "compute-update"),
        Route("POST", "/api/compute/{compute_id}/docker/discover", docker_discover,
              name="compute-docker-discover"),
        Route("POST", "/api/compute/{compute_id}/docker/check", docker_check,
              name="compute-docker-check"),
        Route("POST", "/api/compute/{compute_id}/docker/projects/{project_name}/strategy",
              docker_strategy, AuthPolicy.ADMIN, "compute-docker-strategy"),
        Route("POST", "/api/compute/{compute_id}/docker/projects/{project_name}/update",
              docker_update, AuthPolicy.ADMIN, "compute-docker-update"),
        Route("GET", "/api/compute/{compute_id}", detail, name="compute-detail"),
    )
