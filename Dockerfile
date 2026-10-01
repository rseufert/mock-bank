FROM python:3.12-slim
WORKDIR /app
COPY mockbank ./mockbank
# `pyproject.toml` maps `examples/` onto `mockbank.examples` (#169), so the build
# needs the directory present or setuptools refuses: the image's installed package
# then carries the same modules as the wheel.
COPY examples ./examples
COPY pyproject.toml README.md LICENSE ./
RUN pip install --no-cache-dir .
EXPOSE 8080
ENTRYPOINT ["mock-bank", "--host", "0.0.0.0", "--port", "8080"]
