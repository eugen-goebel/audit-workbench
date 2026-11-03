"""Data side of the dashboard: view models and the run source, without any web framework.

`views` turns a `Run` into plain data (dataclasses and pandas frames); `source` gets the run
from a data folder or from a generated bank. The Streamlit app in the repository's
`dashboard/` folder only renders what these modules return.
"""
