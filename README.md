# NIU Home Assistant integration

> [!WARNING]
> **This fork is vibe-coded.** The changes below were written by an AI (Claude, via Claude Code) on behalf of the repository owner, who is not a professional developer. They were checked with unit tests and a test run in a real Home Assistant instance with mocked NIU data, and are used on the owner's own scooter, but they have not been reviewed line by line by a human. Use at your own risk, and read the code before you rely on it. Commits made by the AI carry a `Co-Authored-By: Claude` trailer.

> Fork of [marcelwestrahome/home-assistant-niu-component](https://github.com/marcelwestrahome/home-assistant-niu-component) with:
> * **A real route map.** NIU's own track thumbnail is drawn on Chinese AMap tiles, which have no streets outside China. The last-track camera now redraws your newest ride on OpenStreetMap-based tiles: from the GPS points of NIU's `/v5/track/detail` endpoint when it returns them, and otherwise (currently the case for overseas accounts, where that endpoint returns no points) by tracing the route line in NIU's thumbnail and placing it on the map using the ride's start and end coordinates. Pick the map style (dark, light or OpenStreetMap standard) under the integration's *Configure* button. If tracing fails, the original thumbnail is shown.
> * **Current location** as a GPS `device_tracker`, so the scooter shows up on the map card.
> * **Swappable batteries.** Every battery that has been in the scooter gets its own device with charge, health and charge-cycle sensors, kept by BMS id, plus a sensor that says which battery is in the scooter now. The last known values of the battery on the shelf are kept when you swap.

This custom component integrates compatible NIU vehicles with Home Assistant.

Now this integration is _asynchronous_ and it is easy installable via config flow.

## Changes:
* It automatically creates a NIU device so its sensors and camera are grouped together.
![auto device](images/niu_integration_device.png)
* If you select the Last track sensor automatically it will create a camera integration, with the rendered image of your last track.
![last track camera](images/niu_integration_camera.png)

With the thanks to pikka97 !!!

## Setup
1. In Home Assistant's settings under "device and services" click on the "Add integration" button.
2. Search for "NIU" and click on it.
3. Enter your NIU account credentials and select the sensors you want to add.
![config flow](images/config_flow_niu_integration.png)
4. Enjoy your new NIU integration :-)

## Known bugs

some people had problems with this version please take the latest 1.o  versions
See https://github.com/marcelwestrahome/home-assistant-niu-component repository
