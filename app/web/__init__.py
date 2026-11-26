"""Web search as an outside source of diagnostic hints (project doc web-search-design.md).

Code decides when to search (the engine's "no verified data" path, a fault code known), builds the
query from the vehicle and the code only, asks Exa (Tavily if Exa fails), and marks every result
fits / check / drop for this car before the model sees it. Off unless EXTERNAL_SOURCE=web.
"""
