import dash
from dash import html, callback, dcc
import dash_bootstrap_components as dbc
from dash.dependencies import Input, Output, State
from dash_ag_grid import AgGrid

from geochem import queries


dash.register_page(__name__, path="/byproduct", name="By-Product Data")

DEFAULT_COL_DEF = {"resizable": True, "sortable": True, "filter": True}
GRID_STYLE = {"width": "100%", "height": "50vh"}

layout = html.Div(
    [
        dcc.Location(id="url-byproduct", refresh=True),
        dbc.Row(
            [
                dbc.Col(
                    [
                        dbc.Label("Commodity"),
                        dbc.Spinner(
                            dcc.Dropdown(
                                id="bp-commodity",
                                options=[],
                                placeholder="Search Commodity",
                            ),
                        ),
                    ],
                    width=4,
                ),
                dbc.Col(
                    [
                        dbc.Label("Country"),
                        dcc.Dropdown(
                            id="bp-country",
                            options=[],
                            placeholder="Search Country",
                        ),
                    ],
                    width=4,
                ),
            ]
        ),
        html.Br(),
        html.H5("Papers"),
        dbc.Row(
            dbc.Spinner(
                html.Div(id="bp-papers-results"),
                color="primary",
                type="border",
                size="lg",
            )
        ),
        html.Br(),
        html.H5("Deposits"),
        dbc.Row(
            dbc.Spinner(
                html.Div(id="bp-deposits-results"),
                color="primary",
                type="border",
                size="lg",
            )
        ),
        html.Br(),
        html.H5("Samples"),
        dbc.Row(
            dbc.Spinner(
                html.Div(id="bp-samples-results"),
                color="primary",
                type="border",
                size="lg",
            )
        ),
    ],
    style={"margin": "20px"},
)


def _no_results_alert():
    return dbc.Alert("No results found.", color="danger")


def _build_papers_grid(papers):
    rows = []
    for p in papers:
        row = dict(p)
        row["doi_link"] = f"[{p['doi']}](https://doi.org/{p['doi']})" if p.get("doi") else None
        rows.append(row)

    column_defs = [
        {"headerName": "Title", "field": "title"},
        {"headerName": "Journal", "field": "journal"},
        {"headerName": "Year", "field": "year"},
        {
            "headerName": "DOI",
            "field": "doi_link",
            "cellRenderer": "markdown",
            "cellRendererParams": {"linkTarget": "_blank"},
        },
    ]
    return AgGrid(
        id="bp-papers-grid",
        style=GRID_STYLE,
        columnDefs=column_defs,
        rowData=rows,
        columnSize="responsiveSizeToFit",
        defaultColDef=DEFAULT_COL_DEF,
        dashGridOptions={
            "rowSelection": "single",
            "pagination": True,
            "paginationPageSize": 20,
            "suppressFieldDotNotation": True,
            "enableCellTextSelection": True,
            "getRowId": {"function": "params.data.paper_uri"},
        },
    )


def _build_deposits_grid(deposits):
    column_defs = [
        {"headerName": "Deposit Name", "field": "deposit_name"},
        {"headerName": "Country", "field": "country"},
        {"headerName": "State", "field": "state"},
        {"headerName": "Deposit Type", "field": "deposit_type_text"},
        {"headerName": "Confidence", "field": "deposit_type_confidence"},
    ]
    return AgGrid(
        id="bp-deposits-grid",
        style=GRID_STYLE,
        columnDefs=column_defs,
        rowData=deposits,
        columnSize="responsiveSizeToFit",
        defaultColDef=DEFAULT_COL_DEF,
        dashGridOptions={
            "rowSelection": "single",
            "pagination": True,
            "paginationPageSize": 20,
            "suppressFieldDotNotation": True,
            "enableCellTextSelection": True,
            "getRowId": {"function": "params.data.site_uri"},
        },
    )


def _build_samples_grid(samples):
    column_defs = [
        {"headerName": "Sample ID", "field": "sample_id"},
        {"headerName": "Sample Name", "field": "sample_name"},
        {"headerName": "Sample Mineral", "field": "mineral"},
        {"headerName": "Analysis ID", "field": "analysis_id"},
        {"headerName": "Grade(ppm)", "field": "grade"},
        {"headerName": "Method", "field": "analytical_method"},
    ]
    return AgGrid(
        id="bp-samples-grid",
        style=GRID_STYLE,
        columnDefs=column_defs,
        rowData=samples,
        columnSize="responsiveSizeToFit",
        defaultColDef=DEFAULT_COL_DEF,
        dashGridOptions={
            "pagination": True,
            "paginationPageSize": 20,
            "suppressFieldDotNotation": True,
            "enableCellTextSelection": True,
            "getRowId": {"function": "params.data.measurement_id.toString()"},
        },
    )


@callback(
    Output("bp-commodity", "options"),
    Input("url-byproduct", "pathname"),
)
def bp_load_commodities(pathname):
    return [{"label": c, "value": c} for c in queries.get_commodities()]


@callback(
    Output("bp-country", "options"),
    Output("bp-papers-results", "children"),
    Output("bp-deposits-results", "children"),
    Output("bp-samples-results", "children"),
    Input("bp-commodity", "value"),
    Input("bp-country", "value"),
    prevent_initial_call=True,
)
def bp_update_papers(commodity, country):
    if not commodity:
        return [], html.Div(), html.Div(), html.Div()

    country_options = [{"label": c, "value": c} for c in queries.get_countries(commodity)]

    papers = queries.get_papers(commodity, country)
    papers_grid = _build_papers_grid(papers) if papers else _no_results_alert()

    return country_options, papers_grid, html.Div(), html.Div()


@callback(
    Output("bp-deposits-results", "children", allow_duplicate=True),
    Output("bp-samples-results", "children", allow_duplicate=True),
    Input("bp-papers-grid", "selectedRows"),
    State("bp-commodity", "value"),
    prevent_initial_call=True,
)
def bp_update_deposits(selected_rows, commodity):
    if not selected_rows or not commodity:
        return html.Div(), html.Div()

    paper_uri = selected_rows[0]["paper_uri"]
    deposits = queries.get_deposits(paper_uri, commodity)
    deposits_grid = _build_deposits_grid(deposits) if deposits else _no_results_alert()

    return deposits_grid, html.Div()


@callback(
    Output("bp-samples-results", "children", allow_duplicate=True),
    Input("bp-deposits-grid", "selectedRows"),
    State("bp-commodity", "value"),
    prevent_initial_call=True,
)
def bp_update_samples(selected_rows, commodity):
    if not selected_rows or not commodity:
        return html.Div()

    site_uri = selected_rows[0]["site_uri"]
    samples = queries.get_samples(site_uri, commodity)

    return _build_samples_grid(samples) if samples else _no_results_alert()
