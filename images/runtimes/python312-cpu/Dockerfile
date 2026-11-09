FROM harbor.striding.internal/docker-hub/library/python@sha256:1148dbb014615300fa7e713b2f94f550c0d1c69780c0805f84f093ceee34dd0b
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
RUN python -c "import sys; assert sys.version_info[:2] == (3, 12)"
RUN test -f /usr/share/zoneinfo/Asia/Shanghai \
    && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
    && echo Asia/Shanghai > /etc/timezone
