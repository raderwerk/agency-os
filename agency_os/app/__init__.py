"""De Spil zelf: het proces dat de werkplaats laat lopen.

    cli.py         de zes subcommando's en de afloopcodes
    config.py      env + ~/.config/raderwerk/spil.env, gevalideerd bij het opstarten
    scheduler.py   de cyclus: poll, schakelaars, poorten, claimen, routeren
    runs.py        één run: de opdracht, de uitvoering en de drie schrijfacties terug
    routing.py     de routeringstabel als data, plus de lusdetectie
    prompts.py     skelet + rolblok + issue + repo-instructies + uitvoercontract
    logbook.py     het handelingenlogboek als jsonl, ook MutationSink op de client
    heartbeat.py   de hartslag en de onafhankelijke wachthond

De modules worden rechtstreeks geïmporteerd (`from agency_os.app import scheduler`).
Dit pakket importeert zelf niets, zodat een los onderdeel bruikbaar blijft zonder
dat de hele keten naar Linear en de uitvoerders mee moet komen.
"""
