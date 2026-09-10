import warnings

# fastmcp (lazy-imported by search_views) still uses authlib.jose; suppress until upstream migrates to joserfc.
warnings.filterwarnings(
    "ignore",
    message="authlib.jose module is deprecated",
)

import tools
from mcp_instance import mcp

if __name__ == "__main__":
    mcp.run()