import os

# Keep GUI tests independent of the developer's live Omarchy theme; the
# Omarchy loader is tested with explicit theme directories instead.
os.environ["AVD_OMARCHY"] = "0"
