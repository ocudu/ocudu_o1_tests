FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/tests

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["pytest", "-vv", "--maxfail=1", "--disable-warnings"]
