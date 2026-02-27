import time
from datetime import datetime, timedelta

def wait_until_targets(target_minutes):
    now = datetime.now()
    current_min = now.minute
    
    # Find the next target minute
    # We look for the first target greater than current_min
    future_targets = [t for t in target_minutes if t > current_min]
    
    if future_targets:
        # Next target is later this hour
        next_min = min(future_targets)
        target_time = now.replace(minute=next_min, second=0, microsecond=0)
    else:
        # Next target is the first one in the NEXT hour
        next_min = min(target_minutes)
        target_time = (now + timedelta(hours=1)).replace(minute=next_min, second=0, microsecond=0)

    # Calculate sleep duration
    delay = (target_time - now).total_seconds()
    delay += 2
    
    print(f"Current time: {now.strftime('%H:%M:%S')}")
    print(f"Next run at:  {target_time.strftime('%H:%M:%S')} (In {round(delay, 2)} seconds)")
    
    # Standard time.sleep can be slightly inaccurate over long periods, 
    # but for 5-30 minute windows, it works perfectly.
    time.sleep(delay)

MY_SCHEDULE = [0]

wait_until_targets(MY_SCHEDULE)
print(datetime.now())