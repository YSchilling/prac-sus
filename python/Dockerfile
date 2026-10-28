#   docker build -t powermodel-python:dev examples/python
FROM python:3.13-alpine
COPY powermodel.py coef.json /app/
USER nobody
ENTRYPOINT ["python3", "/app/powermodel.py"]
