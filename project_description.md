TGCat: A database of transmission grating spectra
=================================================
We are building a website to serve the data products of the Chandra Transmission Grating Catalog (TGCat). The website will allow users to search for and download spectra, light curves, and other data products from the TGCat database. The website will also provide tools for visualizing and analyzing the data.

The most important capabilities will be enumerated below:

- Search for and download spectra, light curves, and other data products from the TGCat database.
    - search by object name, coordinates, observation ID, and other criteria
- display a list of objects or observations matching the search criteria
- display a list of data products available for each object or observation
- select and download data products in various formats (FITS)
- visualize spectra and light curves in the browser

Scope
-----
The apps and services in this project will be deployed on a production server. The underlying SQL database and all associated files (images, spectra, light curves etc.) will be produced and managed on a separate server with separate infrastructure. Thus, in this project, there is no need for database administration, user accounts, data updates etc. The database will be pushed nightly to the production server and is simply to be served by the code written here.

Note that the database pushed may contain fields and data that is not needed for this purpose and that are simply left in from the separate service and produces the database.

Technology Stack
-----------------
The website will be built using the following technologies:
- Python
- Django
- MySQL

Priorities
----------
Searching, selecting, downloading data and displaying premade png images of spectra and light curves are the highest priority. The ability to visualize spectra and light curves in the browser is a lower priority, and will be implemented only if time allows.