"""OpenHippo CLI."""

import click

@click.group()
@click.version_option()
def main():
    """🦛 OpenHippo — Local-first memory engine for AI Agents."""
    pass

@main.command()
@click.option("--host", default="127.0.0.1")
@click.option("--port", default=8200)
def serve(host: str, port: int):
    """Start the REST API server."""
    click.echo(f"🦛 OpenHippo server starting on {host}:{port}")
    import uvicorn
    uvicorn.run("openhippo.api.server:app", host=host, port=port)

@main.command()
def mcp():
    """Start as MCP tool server (stdio)."""
    click.echo("🦛 OpenHippo MCP server starting...")

if __name__ == "__main__":
    main()
