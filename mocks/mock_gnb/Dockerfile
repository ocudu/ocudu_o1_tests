FROM python:3.11-slim

ENV PIP_NO_CACHE_DIR=1

WORKDIR /opt/mock-gnb

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mock_gnb.py .

ENTRYPOINT ["python", "mock_gnb.py"]
