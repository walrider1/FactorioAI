"""The brain bot: an LLM thinks ahead, a scripted crew builds in the background.

    strategist (LLM, rare)  -> goals: what to build and research next, and a standing plan (doctrine)
    scheduler  (code)       -> turns goals into jobs: craft what a module needs, gather what crafting needs
    modules    (code)       -> chiplets: burner mines, steam power, labs, whole factories (the fab engine)
    crew       (code)       -> walks, mines, crafts, places, at the game's rate limit
    foreman    (code)       -> keeps it running: fuel, coal restock, plates collected, labs fed, problems reported

See bots/brain/README.md for the design and where it goes next.
"""
