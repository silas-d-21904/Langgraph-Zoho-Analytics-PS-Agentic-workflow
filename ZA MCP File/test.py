from tools.metadata_tools import get_workspaces_list
import asyncio


asyncio.run(get_workspaces_list(
  include_shared_workspaces= False,
  contains_str="Bugs Tracker Dataset - Destination"
))
