FROM harbor.striding.internal/data/data-operators/base/runtime/python312-cpu:1.0.1 AS runtime

FROM runtime AS dependency-build
USER root
COPY wheelhouse/dependencies/ /build/dependencies/
COPY requirements.dependencies.txt /build/requirements.dependencies.txt
RUN python -m venv /opt/operator/venv \
    && /opt/operator/venv/bin/python -m pip --isolated install --no-index --no-deps \
       --no-cache-dir --only-binary=:all: --require-hashes --find-links=/build/dependencies \
       -r /build/requirements.dependencies.txt
FROM dependency-build AS project-build
COPY wheelhouse/projects/ /build/projects/
COPY requirements.projects.txt /build/requirements.projects.txt
RUN /opt/operator/venv/bin/python -m pip --isolated install --no-index --no-deps --prefix=/opt/project \
       --no-cache-dir --only-binary=:all: --require-hashes --find-links=/build/projects \
       -r /build/requirements.projects.txt

FROM runtime AS final
WORKDIR /app
COPY --from=dependency-build /opt/operator/venv /opt/operator/venv
COPY --from=project-build /opt/project/ /opt/operator/venv/
RUN /opt/operator/venv/bin/python -m pip check
COPY main.py operator.yaml /app/
ENV PATH="/opt/operator/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 10001:10001
ENTRYPOINT ["python", "/app/main.py"]
