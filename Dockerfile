FROM python:3.12-slim
WORKDIR /src
COPY . .
RUN pip install .
CMD ["python", "-m", "app"]
