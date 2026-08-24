# HPC MCP Server Dockerfile
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies (including Slurm client if needed, 
# but usually we assume the host has it or we are monitoring local resources)
RUN apt-get update && apt-get install -y \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install MCP
RUN pip install --no-cache-dir "mcp[cli]" fastapi uvicorn

# Copy the server code
COPY hpc_mcp_server/ ./hpc_mcp_server/
COPY pyproject.toml .
COPY README.md .

# Set environment variables
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1

# Expose port 8223
EXPOSE 8223

# Default command - running as SSE on port 8223
CMD ["python", "hpc_mcp_server/server.py", "--transport", "sse", "--port", "8223"]
