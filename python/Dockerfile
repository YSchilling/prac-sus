#   docker build -t powermodel-python:dev examples/python
FROM python:3.13-alpine
# coef*.json: POWERMODEL_COEF=/app/coef-default-profile.json selects the lab default-tuning fit.
COPY powermodel.py coef*.json /app/
USER nobody
ENTRYPOINT ["python3", "/app/powermodel.py"]
