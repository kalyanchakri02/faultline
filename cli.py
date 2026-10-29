import sys, loggen, agent
name = sys.argv[1] if len(sys.argv) > 1 else "ecc_remap_exhausted"
for ev in agent.triage(loggen.generate(name, 7, int(sys.argv[2]) if len(sys.argv) > 2 else 0)):
    t = ev["type"]
    if t == "parser_attempt": print(f"\n--- parser attempt {ev['attempt']} ({ev['kind']}) ok={ev['ok']} ---\n{ev['problems']}")
    elif t == "code": print(f"\n--- step {ev['step']} CODE ---\n{ev['code']}")
    elif t == "result": print(f"--- RESULT (rc={ev['exit_code']}) ---\n{ev['stdout']}{ev['stderr']}")
    else: print(f"\n[{t}]", {k: v for k, v in ev.items() if k != 'type'})
print("\nTRUTH:", loggen.SCENARIOS[name])
