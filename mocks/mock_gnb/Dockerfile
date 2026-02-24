#
# Copyright 2021-2026 Software Radio Systems Limited
#
# By using this file, you agree to the terms and conditions set
# forth in the LICENSE file which can be found at the top level of
# the distribution.
#

FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/mock-gnb

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mock_gnb.py .

ENTRYPOINT ["python", "mock_gnb.py"]
