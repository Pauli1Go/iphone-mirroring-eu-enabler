import plistlib
import subprocess


# Path to your .plist file
file_path = "/private/var/db/os_eligibility/eligibility.plist"
app_path = '/System/Applications/iPhone Mirroring.app'

# Load the plist file
with open(file_path, 'rb') as file:
    plist_data = plistlib.load(file)

# Navigate to the specific section you want to update
if "OS_ELIGIBILITY_DOMAIN_IRON" in plist_data:
    dict_data = plist_data["OS_ELIGIBILITY_DOMAIN_IRON"]
    
    # Update the values as needed
    if "os_eligibility_answer_t" in dict_data:
        dict_data["os_eligibility_answer_t"] = 4  # Replace 2 with 4

    if "status" in dict_data and isinstance(dict_data["status"], dict):
        status_dict = dict_data["status"]
        status_dict["OS_ELIGIBILITY_INPUT_COUNTRY_BILLING"] = 3  # Replace 2 with 3
        status_dict["OS_ELIGIBILITY_INPUT_COUNTRY_LOCATION"] = 3  # Replace 2 with 3
        # "OS_ELIGIBILITY_INPUT_DEVICE_CLASS" already has 3, no need to change

# Save the updated plist data back to the file
with open(file_path, 'wb') as file:
    plistlib.dump(plist_data, file)

print("Plist file updated successfully!")

subprocess.run(["open", app_path])
