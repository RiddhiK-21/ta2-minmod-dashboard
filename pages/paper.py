from urllib.parse import unquote

import dash
from dash import html
import dash_bootstrap_components as dbc
import pandas as pd
from dash_ag_grid import AgGrid

dash.register_page(__name__, path_template="/paper/<site_name>", name="Papers")


def layout(site_name=None, **kwargs):
    display_name = unquote(site_name) if site_name else "Unknown Site"

    # Placeholder rows until papers are linked to their source mineral sites
    df = pd.DataFrame(
        [
            {
                "Paper Title": "Placeholder Paper Title 1",
                "Paper Link": "https://example.com/paper/p1",
                "Authors": " Author Names",
                "Year": "N/A",
            },
            {
                "Paper Title": "Placeholder Paper Title 2",
                "Paper Link": "https://example.com/paper/p2",
                "Authors": "Author Names",
                "Year": "N/A",
            },
        ]
    )

    column_defs = []
    for col in df.columns:
        if col == "Paper Link":
            column_defs.append(
                {"headerName": col, "field": col, "cellRenderer": "urlLink"}
            )
        else:
            column_defs.append({"headerName": col, "field": col})

    return html.Div(
        [
            html.H4(f"{display_name} Papers"),
            html.Br(),
            AgGrid(
                id="paper_table",
                style={"width": "100%", "height": "50vh"},
                columnDefs=column_defs,
                rowData=df.to_dict("records"),
                columnSize="responsiveSizeToFit",
                defaultColDef={"resizable": True, "sortable": True, "filter": True},
                dashGridOptions={
                    "pagination": True,
                    "paginationPageSize": 20,
                    "enableCellTextSelection": True,
                },
            ),
        ],
        style={"margin": "20px"},
    )
