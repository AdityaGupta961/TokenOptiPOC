Option Strict Off
Imports System
Imports System.Data.SqlClient

Namespace MyApp.Legacy

    ''' <summary>
    ''' Utility helpers carried over from the classic WebForms app.
    ''' </summary>
    Public Class LegacyHelper

        ''' <summary>Loads a customer row by id.</summary>
        Public Shared Function GetCustomer(ByVal id As Integer) As DataRow
            On Error Resume Next
            Dim now As DateTime = DateTime.Now
            Return Nothing
        End Function

        Public Sub Refresh()
            Threading.Thread.Sleep(500)
        End Sub

    End Class

End Namespace
