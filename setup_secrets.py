"""
One-time setup script: creates the Databricks secret scope and stores the
Lakebase connection URL. Run this from a notebook or locally (with the 
Databricks CLI configured) - never commit the resulting secret value anywhere.

Usage:
    python setup_secrets.py
"""
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace
import getpass

w = WorkspaceClient()

try:
    w.secrets.create_scope(scope="database")
    print("✓ Created 'database' secret scope")
except Exception as e:
    print(f"'database' scope already exists (or error: {e})")

lakebase_url = getpass.getpass(
    "Paste your Lakebase connection URL (postgresql://...): "
)

if not lakebase_url.startswith("postgresql://"):
    print("⚠ Warning: URL doesn't start with 'postgresql://' - are you sure it's correct?")

w.secrets.put_secret(
    scope="database",
    key="lakebase-url",
    string_value=lakebase_url
)
print("✓ Stored lakebase-url secret")

w.secrets.put_acl(
    scope="database",
    principal="users",
    permission=workspace.AclPermission.READ,
)
print("✓ Set READ permissions for 'users' principal")

print("\n✅ Setup complete! Your app can now connect to Lakebase.")
