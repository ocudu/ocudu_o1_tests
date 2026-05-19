# SPDX-FileCopyrightText: Copyright (C) 2021-2026 Software Radio Systems Limited
# SPDX-License-Identifier: BSD-3-Clause-Open-MPI

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/mock-smo

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mock_smo.py .
COPY payloads ./payloads

ENTRYPOINT ["python", "mock_smo.py"]
