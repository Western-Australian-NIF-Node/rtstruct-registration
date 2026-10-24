FROM python:3.13-slim

RUN pip install --no-cache-dir pydicom==3.0.2

WORKDIR /app
COPY register_rt_collections.py /app/

# The XNAT Container Service supplies the command line from command.json,
# so no ENTRYPOINT/CMD is needed. This default makes `docker run` self-describing.
CMD ["python", "/app/register_rt_collections.py", "--help"]
