# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-FileCopyrightText: Copyright (C) 2026 OCUDU contributors
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/tests

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Docker CLI only (no daemon): the notification-injection tests exec
# `sysrepocfg --notification` in the mock RU container through the daemon
# socket the compose file mounts.
ARG DOCKER_CLI_VERSION=29.8.2
ARG DOCKER_CLI_ARCH=x86_64
ADD https://download.docker.com/linux/static/stable/${DOCKER_CLI_ARCH}/docker-${DOCKER_CLI_VERSION}.tgz /tmp/docker.tgz
RUN tar -xzf /tmp/docker.tgz -C /tmp docker/docker \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && rm -rf /tmp/docker /tmp/docker.tgz \
    && docker --version

COPY . .

ENTRYPOINT ["pytest"]
