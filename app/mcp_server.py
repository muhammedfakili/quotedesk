"""The product catalogue as an MCP server.

Any MCP client can use it: the CrewAI matching agent (through crewai-tools'
MCPServerAdapter, over stdio), Claude Desktop, an n8n MCP node, or another team's agent.
The tools are read-only and expose what a salesperson may see: name, unit, pack size,
stock and lead time. Cost prices and supplier names are not reachable through MCP at
all; pricing happens in code (app/pricing.py), never in an agent.

    uv run python -m app.mcp_server                 # stdio (how the app starts it)
    uv run python -m app.mcp_server --http 8765     # streamable HTTP for other clients
"""

from __future__ import annotations

import argparse
import logging

from mcp.server.fastmcp import FastMCP

from app.catalogue.index import CATEGORIES, get_catalogue

mcp = FastMCP(
    "northgate-catalogue",
    instructions=(
        "Product catalogue of Northgate Industrial Supply. Search by description (trade shorthand "
        "like 'SS', 'BZP', '1/2\"', 'M10x50' is understood). Results never include prices."
    ),
)


@mcp.tool()
def search_products(query: str, k: int = 5, category: str | None = None) -> dict:
    """Find catalogue products that match a free-text description.

    Hybrid search (keyword + semantic). Each result has sku, name, category, unit,
    pack_qty, base_unit, stock, lead_days, a relevance score and `why` (which search
    found it and which terms matched). `category` optionally limits the search to one of:
    fasteners, pipe_fittings, valves, electrical, ppe, tools_consumables, sealants_adhesives.
    """
    if category is not None and category not in CATEGORIES:
        return {"error": f"unknown category '{category}'", "categories": list(CATEGORIES)}
    k = max(1, min(int(k), 10))
    hits = get_catalogue().search(query, k=k, category=category)
    return {"query": query, "results": [h.as_dict() for h in hits]}


@mcp.tool()
def get_product(sku: str) -> dict:
    """Details of one product by SKU (unit, pack size, stock, lead time). No price."""
    product = get_catalogue().get(sku.strip().upper())
    if product is None:
        return {"error": f"no product with sku '{sku}'"}
    return product.public()


@mcp.tool()
def check_stock(sku: str, quantity: float) -> dict:
    """Whether `quantity` sell units of a product are in stock, and the lead time if not."""
    product = get_catalogue().get(sku.strip().upper())
    if product is None:
        return {"error": f"no product with sku '{sku}'"}
    return {"sku": product.sku, "unit": product.unit, "requested": quantity, "in_stock": product.stock,
            "available": product.stock >= quantity, "lead_days": product.lead_days}


@mcp.tool()
def list_categories() -> dict:
    """The fixed product categories and their labels."""
    return {"categories": CATEGORIES}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP on this port")
    args = parser.parse_args()
    logging.getLogger("mcp").setLevel(logging.WARNING)
    get_catalogue().warm()  # load the embeddings before the first call, not during it
    if args.http:
        mcp.settings.port = args.http
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
