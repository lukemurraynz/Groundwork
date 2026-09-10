// Monthly cost budget for the Groundwork platform subscription.
//
// Motivation: the live environment has already been lost once to sponsorship-credit exhaustion
// (the project's internal implementation notes (not included in this release), 2026-08-21 entry). This module makes the
// spend ceiling explicit and pages the operator before Azure pulls the plug. It is inactive by
// default - set `budgetContactEmail` via azd env / parameters to activate.
//
// API version 2019-10-01: newest stable Consumption budgets version per the repo rule of
// verifying against `az provider show --namespace Microsoft.Consumption`; the 2024 preview is
// deliberately not used here.

@description('Display name for the budget.')
param budgetName string = 'groundwork-monthly'

@description('Monthly amount in the billing currency.')
param amount int = 200

@description('Operator e-mail that receives threshold notifications. Empty string disables the budget entirely.')
param contactEmail string = ''

@description('Budget period start (first day of a month). Defaults are updated at activation time; budgets roll monthly thereafter.')
param startDate string = '2026-09-01'

var thresholds = [
  {
    threshold: 80
    operator: 'GreaterThan'
  }
  {
    threshold: 100
    operator: 'GreaterThanOrEqualTo'
  }
]

resource budget 'Microsoft.Consumption/budgets@2019-11-01' = if (!empty(contactEmail)) {
  name: budgetName
  properties: {
    category: 'Cost'
    amount: amount
    timeGrain: 'Monthly'
    timePeriod: {
      startDate: startDate
    }
    notifications: [for t in thresholds: {
      enabled: true
      operator: t.operator
      threshold: t.threshold
      contactEmails: [
        contactEmail
      ]
      thresholdType: 'Actual'
    }]
  }
}

output budgetName string = contactEmail == '' ? '' : budgetName
