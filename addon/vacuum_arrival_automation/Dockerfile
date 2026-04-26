ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.13-alpine3.22
FROM ${BUILD_FROM}

RUN apk add --no-cache bash \
    && pip install --no-cache-dir appdaemon pyyaml

COPY run.sh /run.sh
COPY render_addon_config.py /opt/vacuum_automation/render_addon_config.py
COPY redirect_dashboard.py /opt/vacuum_automation/redirect_dashboard.py
COPY dashboard.html /opt/vacuum_automation/dashboard.html
COPY app /opt/vacuum_automation/app
COPY dashboards /opt/vacuum_automation/dashboards

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
