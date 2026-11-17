# sqlite-app workload image: Python standard library only, no package installs.
#
#   docker build -t sqlite-app:v1 workloads/sqlite-app       # from the repo root
#
# The lab Job pulls it from registry.lab:5010; see README "Run in the lab".
#
# python:3.13-slim (Debian 13), pinned by digest; it ships SQLite 3.46.1.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY --chmod=0444 app.py schema.sql /app/
COPY --chmod=0555 run.sh /app/
USER 65534:65534
ENTRYPOINT ["/app/run.sh"]
CMD ["/data/sqlite-app"]
