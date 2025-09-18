import json
import re

# Tank mapping
tank_map = {
    "APR362": "tank1",
    "APR348": "tank2",
    "APR332": "tank3",
    "APR338": "tank4",
    "APR339": "tank5",
    "APR360": "tank6",
    "APR333": "tank7",
    "APR381": "tank8",
    "APR428": "tank9",
    "APR432": "tank10",
    "APR359": "tank11",
    "APR397": "tank12",
    "APR356": "tank13",
    "APR331": "tank14",
    "APR340": "tank15",
    "APR317": "tank17",
    "APR322": "tank18",
    "APR351": "tank19",
    "APR363": "tank20",
    "APR344": "tank22"
}

# Read process_steps.txt
with open('process_steps.txt', 'r') as file:
    lines = file.readlines()

process_data = {}
step_counter = 1  # Increment this for every stirrer

for line in lines:
    # Match stirrer lines
    stirrer_match = re.match(
        r"Stirrer (\d+): (\w+) \| [\d.]+% \| ([\d.]+kg) \| Start: ([\d.]+) \| End: (.*?)\| Category: (.+)",
        line.strip()
    )
    if stirrer_match:
        stirrer_num = f"stirrer{stirrer_match.group(1)}"
        chemical = stirrer_match.group(2)
        weight = stirrer_match.group(3)
        start_time = stirrer_match.group(4)
        end_time = stirrer_match.group(5).strip()
        tank = tank_map.get(chemical, "unknown")

        # Create step entry
        step_key = f"step{step_counter}"
        process_data[step_key] = {
            "stirrer": stirrer_num,
            "tank": tank,
            "chemical": chemical,
            "weight": weight,
            "start": start_time,
            "end": end_time
        }
        step_counter += 1

# Write to JSON file
with open('process_data.json', 'w') as json_file:
    json.dump(process_data, json_file, indent=4)

print("✅ process_data.json created successfully.")
