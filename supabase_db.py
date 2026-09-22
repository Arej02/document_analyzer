import os
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()
url: str = os.environ.get("SUPABASE_URL")
key: str = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase= create_client(url, key)
print("Supabase connection created successfully!")