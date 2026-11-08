FROM python:3.13-slim AS icons
RUN pip install --no-cache-dir pillow==11.0.0
COPY app/make_icons.py /make_icons.py
RUN mkdir /out && python /make_icons.py /out

FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && useradd -u 1000 -m pantry
WORKDIR /app
COPY app/pantry /app/pantry
COPY --from=icons /out/ /app/pantry/static/
RUN printf '#!/bin/sh\ncd /app && exec python -m pantry.cli "$@"\n' > /usr/local/bin/pantry && chmod 755 /usr/local/bin/pantry
USER 1000
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"
CMD ["uvicorn", "pantry.main:app", "--host", "0.0.0.0", "--port", "8000"]
