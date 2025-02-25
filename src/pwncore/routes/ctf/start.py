from __future__ import annotations

from logging import getLogger

from fastapi import APIRouter, Response
import uuid
from tortoise.transactions import in_transaction

from pwncore.models import Problem, Container, Ports
from pwncore.container import docker_client
from pwncore.config import config
from pwncore.routes.auth import RequireJwt

router = APIRouter(tags=["ctf"])
logger = getLogger(__name__)


@router.post("/{ctf_id}/start")
async def start_docker_container(ctf_id: int, response: Response, jwt: RequireJwt):
    """
    image_config contains the raw POST data that gets sent to the Docker Remote API.
    For now it just contains the guest ports that need to be opened on the host.
    image_config:
    {
        "PortBindings": {
            "22/tcp": [{}]      # Let docker randomly assign ports
        }
    }
    """
    async with in_transaction():
        ctf = await Problem.get_or_none(id=ctf_id, visible=True)
        if not ctf:
            response.status_code = 404
            return {"msg_code": config.msg_codes["ctf_not_found"]}

        team_id = jwt["team_id"]  # From JWT
        team_container = await Container.get_or_none(team=team_id, problem=ctf_id)
        if team_container:
            db_ports = await team_container.ports.all().values(
                "port"
            )  # Get ports from DB
            ports = [db_port["port"] for db_port in db_ports]  # Create a list out of it
            return {
                "msg_code": config.msg_codes["container_already_running"],
                "ports": ports,
                "ctf_id": ctf_id,
            }

        if (
            await Container.filter(
                team_id=team_id
            ).count() >= config.max_containers_per_team
        ):  # fmt: skip
            return {"msg_code": config.msg_codes["container_limit_reached"]}

        # Start a new container
        container_name = f"{team_id}_{ctf_id}_{uuid.uuid4().hex}"
        container_flag = f"{config.flag}{{{uuid.uuid4().hex}}}"

        # Run
        container = await docker_client.containers.run(
            name=container_name,
            config={
                "Image": ctf.image_name,
                # Detach stuff
                "AttachStdin": False,
                "AttachStdout": False,
                "AttachStderr": False,
                "Tty": False,
                "OpenStdin": False,
                **ctf.image_config,
            },
        )

        await (
            await container.exec(["/bin/bash", "/root/gen_flag", container_flag])
        ).start(detach=True)

        try:
            db_container = await Container.create(
                docker_id=container.id,
                team_id=team_id,
                problem_id=ctf_id,
                flag=container_flag,
            )

            # Get ports and save them
            ports = []  # List to return back to frontend
            for guest_port in ctf.image_config["PortBindings"]:
                # Docker assigns the port to the IPv4 and IPv6 addresses
                # Since we only require IPv4, we select the zeroth item
                # from the returned list.
                port = int((await container.port(guest_port))[0]["HostPort"])
                ports.append(port)
                await Ports.create(port=port, container=db_container)

        except Exception as err:
            # Stop the container if failed to make a DB record
            await container.stop()
            await container.delete()
            logger.exception("Error while starting", exc_info=err)

            response.status_code = 500
            return {"msg_code": config.msg_codes["db_error"]}

        return {
            "msg_code": config.msg_codes["container_start"],
            "ports": ports,
            "ctf_id": ctf_id,
        }


@router.post("/stopall")
async def stopall_docker_container(response: Response, jwt: RequireJwt):
    async with in_transaction():
        team_id = jwt["team_id"]  # From JWT

        containers = await Container.filter(team_id=team_id).values()

        # We first try to delete the record from the DB
        # Then we stop the container
        try:
            await Container.filter(team_id=team_id).delete()
        except Exception:
            response.status_code = 500
            return {"msg_code": config.msg_codes["db_error"]}

        for db_container in containers:
            container = await docker_client.containers.get(db_container["docker_id"])
            await container.stop()
            await container.delete()

        return {"msg_code": config.msg_codes["containers_team_stop"]}


@router.post("/{ctf_id}/stop")
async def stop_docker_container(ctf_id: int, response: Response, jwt: RequireJwt):
    async with in_transaction():
        # Let this work on invisible problems incase
        # we mess up the database while making problems visible
        ctf = await Problem.get_or_none(id=ctf_id)
        if not ctf:
            response.status_code = 404
            return {"msg_code": config.msg_codes["ctf_not_found"]}

        team_id = jwt["team_id"]
        team_container = await Container.get_or_none(team_id=team_id, problem_id=ctf_id)
        if not team_container:
            return {"msg_code": config.msg_codes["container_not_found"]}

        # We first try to delete the record from the DB
        # Then we stop the container
        try:
            await Container.filter(team_id=team_id, problem_id=ctf_id).delete()
        except Exception:
            response.status_code = 500
            return {"msg_code": config.msg_codes["db_error"]}

        container = await docker_client.containers.get(team_container.docker_id)
        await container.stop()
        await container.delete()

        return {"msg_code": config.msg_codes["container_stop"]}
