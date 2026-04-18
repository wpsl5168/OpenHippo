"""OpenHippo CLI."""

from __future__ import annotations

import json
import click


def _engine():
    from .core.engine import HippoEngine
    return HippoEngine()


@click.group()
@click.version_option(package_name="openhippo")
def main():
    """🦛 OpenHippo — Local-first memory engine for AI Agents."""
    pass


@main.command()
@click.option("--host", default="127.0.0.1", help="Bind address")
@click.option("--port", default=8200, help="Port number")
def serve(host: str, port: int):
    """Start the REST API server."""
    click.echo(f"🦛 OpenHippo server starting on {host}:{port}")
    import uvicorn
    uvicorn.run("openhippo.api.rest:app", host=host, port=port, reload=False)


@main.command()
def mcp():
    """Start as MCP tool server (stdio)."""
    from .api.mcp_server import run
    run()


@main.command()
@click.argument("content")
@click.option("--target", "-t", default="memory", type=click.Choice(["memory", "user"]))
@click.option("--json-output", "--json", "as_json", is_flag=True)
def add(content: str, target: str, as_json: bool):
    """Add a memory entry."""
    e = _engine()
    result = e.add(target, content)
    e.close()
    if as_json:
        click.echo(json.dumps(result))
    else:
        click.echo(f"✅ Added [{result['id']}] to {target}")


@main.command()
@click.argument("query")
@click.option("--target", "-t", default=None, type=click.Choice(["memory", "user"]))
@click.option("--source", "-s", default="all", type=click.Choice(["all", "hot", "cold"]))
@click.option("--limit", "-l", default=20)
@click.option("--json-output", "--json", "as_json", is_flag=True)
def search(query: str, target: str | None, source: str, limit: int, as_json: bool):
    """Search memories."""
    e = _engine()
    result = e.search(query, target, source, limit)
    e.close()
    if as_json:
        click.echo(json.dumps(result, default=str, ensure_ascii=False))
    else:
        click.echo(f"Found {result['total']} results for '{query}':")
        for entry in result.get("hot", []):
            click.echo(f"  🔥 [{entry['id'][:8]}] {entry['content'][:80]}")
        for entry in result.get("cold", []):
            click.echo(f"  ❄️  [{entry['id'][:8]}] {entry['content'][:80]}")


@main.command()
@click.option("--target", "-t", default=None, type=click.Choice(["memory", "user"]))
@click.option("--json-output", "--json", "as_json", is_flag=True)
def hot(target: str | None, as_json: bool):
    """List hot memory entries."""
    e = _engine()
    if target:
        entries = e.get_hot(target)
    else:
        entries = e.get_hot("memory") + e.get_hot("user")
    e.close()
    if as_json:
        click.echo(json.dumps(entries, default=str, ensure_ascii=False))
    else:
        for entry in entries:
            icon = "🧠" if entry["target"] == "memory" else "👤"
            click.echo(f"  {icon} [{entry['id'][:8]}] {entry['content'][:80]}")


@main.command()
@click.option("--json-output", "--json", "as_json", is_flag=True)
def stats(as_json: bool):
    """Show memory statistics."""
    e = _engine()
    s = e.stats()
    e.close()
    if as_json:
        click.echo(json.dumps(s))
    else:
        click.echo("🦛 OpenHippo Stats")
        click.echo(f"  Hot memory:  {s['hot_memory_count']} entries ({s['hot_memory_usage']})")
        click.echo(f"  Hot user:    {s['hot_user_count']} entries ({s['hot_user_usage']})")
        click.echo(f"  Cold total:  {s['cold_count']} entries")
        click.echo(f"  DB size:     {s['db_size_kb']} KB")


if __name__ == "__main__":
    main()
