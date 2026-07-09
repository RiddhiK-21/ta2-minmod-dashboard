import dash
from dash import html, callback, dcc
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output
import pandas as pd
from dash_ag_grid import AgGrid
from dash.exceptions import PreventUpdate

from helpers import kpis

dash.register_page(__name__, path="/byproduct", name="By-Product Data")

layout = html.Div(
    [
        dcc.Location(id="url-byproduct", refresh=True),
        # ------------------------ Commodity row ------------------------
        dbc.Row(
            dbc.Col(
                [
                    html.P(
                        "Select Commodity",
                        style={
                            "font-family": '"Open Sans", verdana, arial, sans-serif',
                            "font-size": "15px",
                            "text-align": "center",
                            "font-weight": "bold",
                        },
                    ),
                    dbc.InputGroup(
                        [
                            dcc.Dropdown(
                                id="commodity-byproduct",
                                # The options will be updated by the callback 'update_commodity_dropdown'
                                options=[],
                                multi=True,
                                placeholder="Search Commodity",
                                style={
                                    "width": "300px",
                                    "fontSize": "13px",
                                },
                            ),
                        ],
                        style={"justifyContent": "center"},
                    ),
                ],
                width=6,
                style={
                    "margin": "auto",
                    "text-align": "center",
                },
            ),
            style={"margin-bottom": "10px"},
        ),
        html.Br(),
        # ------------------------ Table row ------------------------
        dbc.Row(
            dbc.Spinner(
                html.Div(id="byproduct-results"),
                color="primary",
                type="border",
                fullscreen=False,
                size="lg",
            )
        ),
    ],
    style={"margin": "20px"},
)


@callback(
    Output("commodity-byproduct", "options"),
    Input(
        "url-byproduct", "pathname"
    ),  # This triggers the callback when the page is refreshed or the URL changes
)
def update_commodity_dropdown(pathname):
    options = [
        {"label": commodity, "value": commodity} for commodity in kpis.get_commodities()
    ]
    return options


@callback(
    Output("byproduct-results", "children"),
    Input("commodity-byproduct", "value"),
    prevent_initial_call=True,
)
def update_byproduct_table(selected_commodity):
    """A callback to render the by-product table once a commodity is selected"""
    if not selected_commodity:
        raise PreventUpdate

    # Placeholder rows until the by-product data source is wired up
    df = pd.DataFrame(
        [
            {
                "Paper Title": "Placeholder Paper Title 1",
                "Mineral Site Name": "Placeholder Mineral Site 1",
                "Grade": "N/A",
                "Edit": "[Edit](#)",
            },
            {
                "Paper Title": "Placeholder Paper Title 2",
                "Mineral Site Name": "Placeholder Mineral Site 2",
                "Grade": "N/A",
                "Edit": "[Edit](#)",
            },
            {
                "Paper Title": "Placeholder Paper Title 3",
                "Mineral Site Name": "Placeholder Mineral Site 3",
                "Grade": "N/A",
                "Edit": "[Edit](#)",
            },
        ]
    )

    return update_grid(df)


def update_grid(df):
    if df is not None and not df.empty:
        column_defs = []
        for col in df.columns:
            if col in ("Mineral Site Name", "Edit"):
                column_defs.append(
                    {
                        "headerName": col,
                        "field": col,
                        "cellRenderer": "markdown",
                        "linkTarget": "_blank",
                    }
                )
            else:
                column_defs.append(
                    {"headerName": col, "field": col, "cellRenderer": "urlLink"}
                )

        column_defs.insert(
            0, {"headerName": "Row ID", "valueGetter": {"function": "params.node.id"}}
        )
        return html.Div(
            AgGrid(
                id="byproduct_table",
                style={"width": "100%", "height": "70vh"},
                columnDefs=column_defs,
                rowData=df.to_dict("records"),
                columnSize="responsiveSizeToFit",
                defaultColDef={"resizable": True, "sortable": True, "filter": True},
                dashGridOptions={
                    "pagination": True,
                    "paginationPageSize": 20,
                    "suppressFieldDotNotation": True,
                    "enableCellTextSelection": True,
                },
                csvExportParams={"fileName": "export_data.csv"},
            )
        )
    return dbc.Alert("No results found.", color="danger")
