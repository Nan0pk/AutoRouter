def total_by_key(rows):
    totals = {}
    for row in rows:
        totals[row["k"]] = totals.get(row["k"], 0) + row["v"]
    for key in totals:
        if totals[key] == 0:
            del totals[key]
    return totals
