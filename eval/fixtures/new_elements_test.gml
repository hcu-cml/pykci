<?xml version="1.0" encoding="UTF-8"?>
<core:CityModel
    xmlns:core="http://www.opengis.net/citygml/2.0"
    xmlns:gml="http://www.opengis.net/gml"
    xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
    xmlns:gen="http://www.opengis.net/citygml/generics/2.0"
    xmlns:xlink="http://www.w3.org/1999/xlink"
    xmlns:test="http://example.com/ade/1.0">
  <gml:name>NewElementsTest</gml:name>
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
      <gml:lowerCorner>0 0 0</gml:lowerCorner>
      <gml:upperCorner>10 10 6</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <core:cityObjectMember>
    <bldg:Building gml:id="BLDG_NEW_1">
      <gml:description>Building exercising newly mapped elements</gml:description>
      <gml:name>Test Building 1</gml:name>
      <gml:metaDataProperty>
        <gml:GenericMetaData>
          <test:author>jdoe</test:author>
        </gml:GenericMetaData>
      </gml:metaDataProperty>
      <gml:boundedBy>
        <gml:Envelope srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
          <gml:lowerCorner>0 0 0</gml:lowerCorner>
          <gml:upperCorner>10 10 6</gml:upperCorner>
        </gml:Envelope>
      </gml:boundedBy>
      <gml:location>
        <gml:Point srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
          <gml:pos>5 5 0</gml:pos>
        </gml:Point>
      </gml:location>
      <core:creationDate>2024-01-01</core:creationDate>
      <core:terminationDate>2030-12-31</core:terminationDate>
      <core:relativeToTerrain>entirelyAboveTerrain</core:relativeToTerrain>
      <core:externalReference>
        <core:informationSystem>http://gdi.example/alkis</core:informationSystem>
        <core:externalObject>
          <core:name>EXT_OBJ_42</core:name>
        </core:externalObject>
      </core:externalReference>
      <core:generalizesTo xlink:href="#BLDG_NEW_1_part0"/>
      <gen:stringAttribute name="Gemeindeschluessel">
        <gen:value>02000000</gen:value>
      </gen:stringAttribute>
      <test:customAttribute>ade-payload-value</test:customAttribute>
      <bldg:class>1000</bldg:class>
      <bldg:function>1000</bldg:function>
      <bldg:usage>1000</bldg:usage>
      <bldg:yearOfConstruction>1985</bldg:yearOfConstruction>
      <bldg:roofType>1000</bldg:roofType>
      <bldg:measuredHeight uom="urn:adv:uom:m">6.0</bldg:measuredHeight>
      <bldg:storeysAboveGround>2</bldg:storeysAboveGround>
      <bldg:storeysBelowGround>1</bldg:storeysBelowGround>
      <bldg:storeyHeightsAboveGround uom="urn:adv:uom:m">3.0 3.0</bldg:storeyHeightsAboveGround>
      <bldg:storeyHeightsBelowGround uom="urn:adv:uom:m">2.5</bldg:storeyHeightsBelowGround>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="WALL_1">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface>
              <gml:surfaceMember>
                <gml:Polygon gml:id="POLY_WALL_1">
                  <gml:exterior>
                    <gml:LinearRing>
                      <gml:posList srsDimension="3">0 0 0 10 0 0 10 0 6 0 0 6 0 0 0</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:lod2MultiCurve>
        <gml:MultiCurve>
          <gml:curveMember>
            <gml:LineString gml:id="MC_LS_1">
              <gml:posList srsDimension="3">0 0 6 10 0 6 10 10 6</gml:posList>
            </gml:LineString>
          </gml:curveMember>
        </gml:MultiCurve>
      </bldg:lod2MultiCurve>
      <bldg:interiorBuildingInstallation>
        <bldg:IntBuildingInstallation gml:id="INTINST_1">
          <gml:name>Staircase</gml:name>
          <bldg:lod4Geometry>
            <gml:MultiSurface>
              <gml:surfaceMember>
                <gml:Polygon gml:id="POLY_INST_1">
                  <gml:exterior>
                    <gml:LinearRing>
                      <gml:posList srsDimension="3">1 1 0 2 1 0 2 1 3 1 1 3 1 1 0</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod4Geometry>
        </bldg:IntBuildingInstallation>
      </bldg:interiorBuildingInstallation>
      <bldg:consistsOfBuildingPart>
        <bldg:BuildingPart gml:id="BLDG_NEW_1_part0">
          <gml:name>Annex</gml:name>
          <bldg:function>1000</bldg:function>
          <bldg:measuredHeight uom="urn:adv:uom:m">3.0</bldg:measuredHeight>
          <bldg:storeysAboveGround>1</bldg:storeysAboveGround>
          <bldg:boundedBy>
            <bldg:RoofSurface gml:id="PART_ROOF_1">
              <bldg:lod2MultiSurface>
                <gml:MultiSurface>
                  <gml:surfaceMember>
                    <gml:Polygon gml:id="POLY_PART_ROOF_1">
                      <gml:exterior>
                        <gml:LinearRing>
                          <gml:posList srsDimension="3">0 0 6 10 0 6 10 10 6 0 10 6 0 0 6</gml:posList>
                        </gml:LinearRing>
                      </gml:exterior>
                    </gml:Polygon>
                  </gml:surfaceMember>
                </gml:MultiSurface>
              </bldg:lod2MultiSurface>
            </bldg:RoofSurface>
          </bldg:boundedBy>
        </bldg:BuildingPart>
      </bldg:consistsOfBuildingPart>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
